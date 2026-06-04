"""
Prioritisation output module.

Produces:
  - JSON: output/watchlist_YYYY-MM-DD.json
  - Markdown: output/watchlist_YYYY-MM-DD.md
"""
import json
import logging
from datetime import date
from pathlib import Path

import config
import db

logger = logging.getLogger(__name__)


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

VALUE_BAND_LABELS = {
    "under_100k":  "< $100k",
    "100k_500k":   "$100k – $500k",
    "500k_2m":     "$500k – $2m",
    "2m_10m":      "$2m – $10m",
    "10m_plus":    "$10m+",
    "unknown":     "Value unknown",
}


def _format_bidder(b: dict) -> str:
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
        value_label = VALUE_BAND_LABELS.get(item.get("value_band") or "unknown", "Value unknown")
        dtc = item.get("days_until_close")
        dtc_str = f"{dtc} days" if dtc is not None else "Unknown"
        close_str = str(item.get("close_date") or "Unknown")
        bidders = _fetch_top_bidders(item["notice_id"])

        red_flags_raw = item.get("red_flags") or ""
        if red_flags_raw:
            flags = [f.strip() for f in red_flags_raw.split(";") if f.strip()]
            flags_md = "\n".join(f"  - {f}" for f in flags) if flags else "  - None identified"
        else:
            flags_md = "  - None identified"

        bidders_md = (
            "\n".join(f"  {i+1}. {_format_bidder(b)}" for i, b in enumerate(bidders))
            if bidders else "  - No bidder data"
        )

        lines += [
            f"## {rank}. {item.get('title') or 'Untitled'} `[{score}/10]`",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| **Agency** | {item.get('agency') or '—'} |",
            f"| **Sector** | {item.get('sector_tag') or '—'} |",
            f"| **Value** | {value_label} |",
            f"| **Close date** | {close_str} ({dtc_str}) |",
            f"| **Scope** | {item.get('geographic_scope') or '—'} |",
            f"| **Notice** | [{item.get('source_url', '')}]({item.get('source_url', '')}) |",
            "",
        ]

        if item.get("summary"):
            lines += [
                "**Summary**",
                "",
                item["summary"],
                "",
            ]

        if item.get("strategic_framing"):
            lines += [
                "**Strategic framing**",
                "",
                f"_{item['strategic_framing']}_",
                "",
            ]

        lines += [
            "**Red flags**",
            "",
            flags_md,
            "",
            "**Likely bidders**",
            "",
            bidders_md,
            "",
            f"_Score reasoning: {item.get('score_reasoning') or '—'}_",
            "",
            "---",
            "",
        ]

    path = output_dir / f"watchlist_{run_date.isoformat()}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("Markdown watchlist written to %s", path)
    return path


# ── Main entry point ──────────────────────────────────────────────────────────

def run_output() -> tuple[Path, Path]:
    logger.info("Generating prioritisation output")
    run_date = date.today()
    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    watchlist = _fetch_watchlist()
    logger.info("%d notices in watchlist", len(watchlist))

    json_path = write_json(watchlist, output_dir, run_date)
    md_path   = write_markdown(watchlist, output_dir, run_date)

    return json_path, md_path
