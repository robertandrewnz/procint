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
from typing import Tuple

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
    "FM":                    "#4f9cf9",
    "infrastructure":        "#f97316",
    "ICT":                   "#a78bfa",
    "advisory":              "#34d399",
    "health":                "#f472b6",
    "security":              "#fb923c",
    "defence":               "#ef4444",
    "utilities":             "#facc15",
    "professional_services": "#38bdf8",
    "other":                 "#94a3b8",
}

IMPORTANCE_COLOURS = {
    "high":   "#22c55e",
    "medium": "#facc15",
    "low":    "#94a3b8",
}

MATURITY_COLOURS = {
    "strong":   "#4f9cf9",
    "moderate": "#fb923c",
    "weak":     "#94a3b8",
}


# ── Data assembly ─────────────────────────────────────────────────────────────

def _fetch_watchlist() -> list[dict]:
    return db.fetchall(
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
        (config.PRIORITY_THRESHOLD, config.TOP_N_WATCHLIST),
    )


def _fetch_top_bidders(notice_id: str) -> list[dict]:
    return db.fetchall(
        """
        SELECT firm_name, size, strategic_importance, intelligence_maturity
        FROM   bidder_pool
        WHERE  notice_id = %s
        ORDER  BY
            CASE strategic_importance
                WHEN 'high'   THEN 1
                WHEN 'medium' THEN 2
                ELSE               3
            END,
            CASE intelligence_maturity
                WHEN 'strong'   THEN 1
                WHEN 'moderate' THEN 2
                ELSE                 3
            END
        LIMIT %s
        """,
        (notice_id, config.TOP_N_BIDDERS_PER_NOTICE),
    )


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
    colour = "#22c55e" if pct >= 70 else "#facc15" if pct >= 45 else "#f97316"
    return f"""
        <div class="score-bar-track">
          <div class="score-bar-fill" style="width:{pct:.1f}%;background:{colour};"></div>
        </div>"""


def _dtc_badge(dtc) -> str:
    if dtc is None:
        return '<span class="badge badge-grey">Close date TBC</span>'
    if dtc <= 7:
        cls = "badge-red"
    elif dtc <= 14:
        cls = "badge-orange"
    elif dtc <= 30:
        cls = "badge-yellow"
    else:
        cls = "badge-green"
    label = "Closes today" if dtc == 0 else f"Closes in {dtc}d"
    return f'<span class="badge {cls}">{label}</span>'


def _sector_badge(sector: str) -> str:
    colour = SECTOR_COLOURS.get(sector, "#94a3b8")
    label = sector.replace("_", " ").upper()
    return f'<span class="sector-badge" style="background:{colour}22;color:{colour};border-color:{colour}44;">{label}</span>'


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


def _bidder_row(b: dict) -> str:
    imp = b.get("strategic_importance", "low")
    mat = b.get("intelligence_maturity", "weak")
    imp_col = IMPORTANCE_COLOURS.get(imp, "#94a3b8")
    mat_col = MATURITY_COLOURS.get(mat, "#94a3b8")
    size = (b.get("size") or "—").capitalize()
    return f"""
          <div class="bidder-row">
            <span class="bidder-name">{b['firm_name']}</span>
            <span class="bidder-meta">{size}</span>
            <span class="bidder-pill" style="color:{imp_col};border-color:{imp_col}44;">▲ {imp}</span>
            <span class="bidder-pill" style="color:{mat_col};border-color:{mat_col}44;">◎ {mat}</span>
          </div>"""


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

    bidders_html = "".join(_bidder_row(b) for b in bidders) if bidders else \
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


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Procurement Intelligence — {run_date}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:        #0d1117;
      --surface:   #161b22;
      --surface2:  #1c2230;
      --border:    #2a3344;
      --text:      #e6edf3;
      --muted:     #7d8fa8;
      --accent:    #4f9cf9;
      --font:      'Inter', system-ui, -apple-system, sans-serif;
    }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      line-height: 1.6;
      padding: 2rem 1.5rem;
    }}

    /* ── Header ── */
    .report-header {{
      max-width: 1100px;
      margin: 0 auto 2.5rem;
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1.25rem;
    }}
    .report-title {{
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--accent);
    }}
    .report-subtitle {{
      font-size: 0.8rem;
      color: var(--muted);
      margin-top: 0.2rem;
    }}
    .report-meta {{
      text-align: right;
      font-size: 0.75rem;
      color: var(--muted);
    }}
    .report-meta strong {{
      display: block;
      font-size: 1.4rem;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.03em;
    }}

    /* ── Cards ── */
    .cards {{
      max-width: 1100px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }}

    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }}

    .card-header {{
      display: flex;
      align-items: flex-start;
      gap: 1rem;
      padding: 1.25rem 1.5rem;
      border-bottom: 1px solid var(--border);
      background: var(--surface2);
    }}

    .rank-badge {{
      flex-shrink: 0;
      width: 2.4rem;
      height: 2.4rem;
      border-radius: 50%;
      background: var(--border);
      color: var(--muted);
      font-size: 0.75rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
      margin-top: 0.15rem;
    }}

    .card-header-main {{
      flex: 1;
      min-width: 0;
    }}

    .card-title-row {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-bottom: 0.25rem;
    }}

    .card-title {{
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--text);
      line-height: 1.4;
    }}

    .card-agency {{
      font-size: 0.78rem;
      color: var(--muted);
    }}

    /* ── Score ── */
    .score-block {{
      flex-shrink: 0;
      text-align: right;
      min-width: 80px;
    }}
    .score-number {{
      font-size: 1.75rem;
      font-weight: 800;
      color: var(--text);
      letter-spacing: -0.04em;
      line-height: 1;
    }}
    .score-label {{
      font-size: 0.7rem;
      color: var(--muted);
      margin-bottom: 0.4rem;
    }}
    .score-bar-track {{
      height: 4px;
      background: var(--border);
      border-radius: 2px;
      overflow: hidden;
      width: 80px;
    }}
    .score-bar-fill {{
      height: 100%;
      border-radius: 2px;
      transition: width 0.3s ease;
    }}

    /* ── Badges ── */
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      white-space: nowrap;
    }}
    .badge-red    {{ background: #ef444422; color: #f87171; border: 1px solid #ef444440; }}
    .badge-orange {{ background: #f9731622; color: #fb923c; border: 1px solid #f9731640; }}
    .badge-yellow {{ background: #facc1522; color: #fde047; border: 1px solid #facc1540; }}
    .badge-green  {{ background: #22c55e22; color: #4ade80; border: 1px solid #22c55e40; }}
    .badge-grey   {{ background: #94a3b822; color: #94a3b8; border: 1px solid #94a3b840; }}

    .sector-badge {{
      display: inline-flex;
      align-items: center;
      padding: 0.2rem 0.55rem;
      border-radius: 4px;
      font-size: 0.65rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      border: 1px solid;
    }}

    /* ── Meta row ── */
    .card-meta-row {{
      display: flex;
      gap: 0;
      border-bottom: 1px solid var(--border);
    }}
    .meta-item {{
      flex: 1;
      padding: 0.65rem 1.5rem;
      border-right: 1px solid var(--border);
    }}
    .meta-item:last-child {{ border-right: none; }}
    .meta-label {{
      display: block;
      font-size: 0.65rem;
      font-weight: 600;
      letter-spacing: 0.07em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 0.2rem;
    }}
    .meta-value {{
      font-size: 0.82rem;
      color: var(--text);
    }}
    .meta-value a {{
      color: var(--accent);
      text-decoration: none;
    }}
    .meta-value a:hover {{ text-decoration: underline; }}

    /* ── Card body — three columns ── */
    .card-body {{
      display: flex;
      gap: 0;
    }}
    .col-intel {{
      flex: 1.8;
      padding: 1.25rem 1.5rem;
      border-right: 1px solid var(--border);
    }}
    .col-actions {{
      flex: 1.4;
      padding: 1.25rem 1.5rem;
      border-right: 1px solid var(--border);
    }}
    .col-bidders {{
      flex: 1;
      padding: 1.25rem 1.5rem;
    }}

    /* ── Recommended actions ── */
    .actions-list {{ display: flex; flex-direction: column; gap: 0.65rem; }}
    .action-item {{
      display: flex;
      align-items: flex-start;
      gap: 0.6rem;
    }}
    .action-num {{
      flex-shrink: 0;
      width: 1.3rem;
      height: 1.3rem;
      border-radius: 50%;
      background: #4f9cf918;
      border: 1px solid #4f9cf940;
      color: var(--accent);
      font-size: 0.65rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
      margin-top: 0.1rem;
    }}
    .action-text {{
      font-size: 0.8rem;
      color: var(--text);
      line-height: 1.6;
    }}

    .section-label {{
      font-size: 0.65rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 0.6rem;
    }}

    .summary-text {{
      font-size: 0.83rem;
      color: var(--text);
      line-height: 1.65;
    }}
    .summary-placeholder {{
      font-size: 0.8rem;
      color: var(--muted);
      font-style: italic;
    }}

    .framing-block {{
      margin-top: 1rem;
      padding: 0.75rem 1rem;
      background: #4f9cf908;
      border-left: 2px solid var(--accent);
      border-radius: 0 4px 4px 0;
    }}
    .framing-label {{
      font-size: 0.65rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--accent);
      display: block;
      margin-bottom: 0.3rem;
    }}
    .framing-block p {{
      font-size: 0.82rem;
      color: var(--text);
      font-style: italic;
    }}

    /* ── Flags ── */
    .flags-list {{ display: flex; flex-direction: column; gap: 0.4rem; }}
    .flag-item {{
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
      font-size: 0.8rem;
      color: var(--text);
    }}
    .flag-icon {{ color: #f97316; flex-shrink: 0; font-style: normal; }}
    .no-flags {{ color: var(--muted); font-style: italic; }}
    .no-flags .flag-icon {{ color: var(--muted); }}

    /* ── Bidders ── */
    .bidders-list {{ display: flex; flex-direction: column; gap: 0.5rem; }}
    .bidder-row {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}
    .bidder-name {{
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--text);
      flex: 1;
      min-width: 0;
    }}
    .bidder-meta {{
      font-size: 0.72rem;
      color: var(--muted);
    }}
    .bidder-pill {{
      font-size: 0.68rem;
      font-weight: 600;
      padding: 0.15rem 0.45rem;
      border-radius: 999px;
      border: 1px solid;
      white-space: nowrap;
    }}

    /* ── Explainer panel ── */
    .explainer {{
      max-width: 1100px;
      margin: 0 auto 2rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.5rem;
      display: grid;
      grid-template-columns: 1fr 1fr 1fr 1fr;
      gap: 1.5rem;
    }}
    .explainer-section h3 {{
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 0.6rem;
    }}
    .explainer-section p {{
      font-size: 0.78rem;
      color: var(--muted);
      line-height: 1.6;
    }}
    .score-breakdown {{
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      margin-top: 0.2rem;
    }}
    .score-dim {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.75rem;
      color: var(--muted);
    }}
    .score-dim-bar {{
      flex: 1;
      height: 3px;
      background: var(--border);
      border-radius: 2px;
      overflow: hidden;
    }}
    .score-dim-fill {{ height: 100%; border-radius: 2px; background: var(--accent); }}
    .score-dim-label {{ min-width: 4.5rem; }}
    .score-dim-weight {{ min-width: 1.5rem; text-align: right; color: #4f9cf9aa; }}

    /* ── Footer ── */
    .report-footer {{
      max-width: 1100px;
      margin: 2rem auto 0;
      padding-top: 1rem;
      border-top: 1px solid var(--border);
      font-size: 0.72rem;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
    }}
  </style>
</head>
<body>

  <div class="report-header">
    <div>
      <div class="report-title">Procurement Intelligence</div>
      <div class="report-subtitle">NZ Government Procurement — Daily Watchlist</div>
    </div>
    <div class="report-meta">
      <strong>{notice_count}</strong>
      opportunities · {run_date}
    </div>
  </div>

  <div class="explainer">
    <div class="explainer-section">
      <h3>What this is</h3>
      <p>A daily watchlist of active NZ government procurement notices from GETS (gets.govt.nz), scored and ranked for strategic relevance to advisory and professional services firms. Notices are ingested each morning and enriched with AI analysis. Use it to prioritise bid/no-bid decisions and outreach.</p>
    </div>
    <div class="explainer-section">
      <h3>How scores are calculated</h3>
      <p>Each notice is scored 1–10 across four dimensions, then combined into a composite weighted score.</p>
      <div class="score-breakdown">
        <div class="score-dim">
          <span class="score-dim-label">Contract value</span>
          <div class="score-dim-bar"><div class="score-dim-fill" style="width:75%"></div></div>
          <span class="score-dim-weight">30%</span>
        </div>
        <div class="score-dim">
          <span class="score-dim-label">Sector priority</span>
          <div class="score-dim-bar"><div class="score-dim-fill" style="width:75%"></div></div>
          <span class="score-dim-weight">30%</span>
        </div>
        <div class="score-dim">
          <span class="score-dim-label">Eval complexity</span>
          <div class="score-dim-bar"><div class="score-dim-fill" style="width:50%"></div></div>
          <span class="score-dim-weight">20%</span>
        </div>
        <div class="score-dim">
          <span class="score-dim-label">Days to close</span>
          <div class="score-dim-bar"><div class="score-dim-fill" style="width:50%"></div></div>
          <span class="score-dim-weight">20%</span>
        </div>
      </div>
    </div>
    <div class="explainer-section">
      <h3>Sector priorities</h3>
      <p>Sectors are ranked by strategic relevance: <strong style="color:#4f9cf9">FM</strong> and <strong style="color:#f97316">Infrastructure</strong> score highest (0.95/0.90), followed by <strong style="color:#ef4444">Defence</strong>, <strong style="color:#fb923c">Utilities</strong>, and <strong style="color:#fb923c">Security</strong> (0.90/0.85), then <strong style="color:#a78bfa">ICT</strong> (0.80), <strong style="color:#34d399">Advisory</strong> (0.70), and other professional services. Adjust weights in config.py to reflect your firm's priorities.</p>
    </div>
    <div class="explainer-section">
      <h3>How to use this report</h3>
      <p>Review the <strong>close date badge</strong> first — red means ≤7 days. Read the <strong>AI summary</strong> and <strong>red flags</strong> to inform go/no-bid. Use <strong>recommended actions</strong> as a starting checklist. Check <strong>likely bidders</strong> to assess competitive field before committing resources.</p>
    </div>
  </div>

  <div class="cards">
{cards_html}
  </div>

  <div class="report-footer">
    <span>Source: GETS (gets.govt.nz) · Scores computed by Procint Layer 1</span>
    <span>Generated {run_date}</span>
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

    path = output_dir / f"watchlist_{run_date.isoformat()}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("HTML watchlist written to %s", path)
    return path


# ── Main entry point ──────────────────────────────────────────────────────────

def run_output() -> Tuple[Path, Path, Path]:
    logger.info("Generating prioritisation output")
    run_date = date.today()
    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    watchlist = _fetch_watchlist()
    logger.info("%d notices in watchlist", len(watchlist))

    json_path = write_json(watchlist, output_dir, run_date)
    md_path   = write_markdown(watchlist, output_dir, run_date)
    html_path = write_html(watchlist, output_dir, run_date)

    return json_path, md_path, html_path
