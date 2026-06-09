"""
Layer 2 Pipeline entry point.

Runs after Layer 1 has completed and appends a "Market Intelligence" section
to the existing daily HTML watchlist.

Steps (in order):
  1. Organisation seeding   — seed/update organisations from Layer 1 data
  2. Organisation discovery — scan notice text for new entity names
  3. Awards ingestion       — scrape GETS contract award notices
  4. Agency profiling       — build/refresh agency intelligence profiles
  5. Pattern detection      — detect renewals, surges, win streaks, spikes
  6. HTML output            — append Market Intelligence section to watchlist

Usage:
  python3 layer2_pipeline.py [--skip-awards] [--skip-profiles]
                              [--company COMPANY_NAME]
  or called from run_pipeline.py with --layer2

The --company flag enables competitor intelligence for the named firm
across the top-scored notices. Defaults to no competitor assessment if omitted.
"""
import argparse
import logging
import sys
from datetime import date

import config  # noqa: must be first so .env is loaded

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            f"layer2_{date.today().strftime('%Y%m%d')}.log"
        ),
    ],
)

logger = logging.getLogger("layer2")

import organisations as orgs_module
import awards as awards_module
import agency_profiles as profiles_module
import patterns as patterns_module
import discovery as discovery_module
import db


# ── HTML Market Intelligence section ─────────────────────────────────────────

_MI_STYLE = """
  <style>
    /* Layer 2 Market Intelligence section */
    .mi-section {
      max-width: 1200px;
      margin: 3rem auto 0;
    }
    .mi-header {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1rem;
      margin-bottom: 2rem;
    }
    .mi-title {
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: #34d399;
    }
    .mi-subtitle { font-size: .8rem; color: var(--muted); margin-top: .2rem; }
    .mi-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.25rem;
      margin-bottom: 2rem;
    }
    .mi-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }
    .mi-card-header {
      padding: .85rem 1.25rem;
      background: var(--surf2);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: .6rem;
    }
    .mi-card-icon { font-size: 1rem; }
    .mi-card-title {
      font-size: .75rem;
      font-weight: 700;
      letter-spacing: .07em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .mi-card-body { padding: 1rem 1.25rem; }

    /* Flags */
    .flag-list { display: flex; flex-direction: column; gap: .6rem; }
    .flag-row {
      display: flex;
      align-items: flex-start;
      gap: .6rem;
      font-size: .8rem;
      line-height: 1.5;
    }
    .flag-sev {
      flex-shrink: 0;
      padding: .15rem .45rem;
      border-radius: 999px;
      font-size: .65rem;
      font-weight: 700;
    }
    .sev-high   { background: #ef444422; color: #f87171; border: 1px solid #ef444440; }
    .sev-medium { background: #facc1522; color: #fde047; border: 1px solid #facc1540; }
    .sev-low    { background: #94a3b822; color: #94a3b8; border: 1px solid #94a3b840; }
    .flag-text  { color: var(--text); }

    /* Org stats */
    .stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: .75rem;
    }
    .stat-box {
      background: var(--surf2);
      border-radius: 6px;
      padding: .65rem .85rem;
    }
    .stat-label {
      font-size: .65rem;
      font-weight: 600;
      letter-spacing: .07em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: .2rem;
    }
    .stat-value {
      font-size: 1.4rem;
      font-weight: 800;
      color: var(--text);
      letter-spacing: -.03em;
    }

    /* Agency profile cards */
    .profile-list { display: flex; flex-direction: column; gap: .75rem; }
    .profile-item {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: .75rem 1rem;
    }
    .profile-name {
      font-size: .85rem;
      font-weight: 600;
      color: var(--text);
      margin-bottom: .25rem;
    }
    .profile-summary {
      font-size: .78rem;
      color: var(--muted);
      line-height: 1.55;
    }
    .profile-meta {
      display: flex;
      gap: .75rem;
      margin-top: .4rem;
      font-size: .72rem;
      color: #4a5568;
    }

    /* Renewal table */
    .renewal-list { display: flex; flex-direction: column; gap: .5rem; }
    .renewal-row {
      display: flex;
      align-items: center;
      gap: .75rem;
      font-size: .8rem;
      border: 1px solid var(--border);
      border-radius: 5px;
      padding: .5rem .75rem;
    }
    .renewal-days {
      flex-shrink: 0;
      font-size: .72rem;
      font-weight: 700;
      padding: .15rem .4rem;
      border-radius: 4px;
      background: #ef444422;
      color: #f87171;
      border: 1px solid #ef444440;
      white-space: nowrap;
    }
    .renewal-days.medium {
      background: #facc1522;
      color: #fde047;
      border-color: #facc1540;
    }
    .renewal-title { flex: 1; color: var(--text); }
    .renewal-parties { color: var(--muted); font-size: .72rem; }

    .mi-empty { font-size: .8rem; color: var(--muted); font-style: italic; }
  </style>
"""


def _flag_row_html(flag: dict) -> str:
    sev = flag.get("severity", "low")
    sev_css = f"sev-{sev}"
    return (
        f'<div class="flag-row">'
        f'<span class="flag-sev {sev_css}">{sev.upper()}</span>'
        f'<span class="flag-text">{flag["description"]}</span>'
        f'</div>'
    )


def _renewal_row_html(flag: dict) -> str:
    desc = flag["description"]
    days_match = __import__("re").search(r"in (\d+) days", desc)
    days = int(days_match.group(1)) if days_match else 999
    css_class = "" if days <= 30 else " medium"
    label = f"{days}d" if days < 999 else "?"
    return (
        f'<div class="renewal-row">'
        f'<span class="renewal-days{css_class}">{label}</span>'
        f'<span class="renewal-title">{desc[:120]}</span>'
        f'</div>'
    )


def _build_market_intelligence_html(
    flags: list[dict],
    agency_profiles: list[dict],
    discovery_stats: dict,
    run_date: date,
) -> str:
    """Build the complete Market Intelligence HTML section."""

    # Split flags by type
    renewals   = [f for f in flags if f["flag_type"] == "approaching_renewal"]
    surges     = [f for f in flags if f["flag_type"] == "procurement_surge"]
    win_streaks = [f for f in flags if f["flag_type"] == "win_streak"]
    spikes     = [f for f in flags if f["flag_type"] == "sector_spike"]
    losses     = [f for f in flags if f["flag_type"] == "loss_streak"]

    other_flags = surges + win_streaks + spikes + losses

    # Stats
    org_stats = db.fetchone(
        """
        SELECT
            COUNT(*) FILTER (WHERE org_type IN ('agency','both')) AS agencies,
            COUNT(*) FILTER (WHERE org_type IN ('bidder','both')) AS bidders,
            (SELECT COUNT(*) FROM contract_awards) AS awards,
            (SELECT COUNT(*) FROM pattern_flags
              WHERE expires_at IS NULL OR expires_at >= CURRENT_DATE) AS active_flags
          FROM organisations
        """
    ) or {}

    # ── Stats strip ──────────────────────────────────────────────────────────
    stats_html = (
        f'<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-label">Agencies tracked</div>'
        f'<div class="stat-value">{org_stats.get("agencies",0)}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Suppliers tracked</div>'
        f'<div class="stat-value">{org_stats.get("bidders",0)}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Awards recorded</div>'
        f'<div class="stat-value">{org_stats.get("awards",0)}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Active flags</div>'
        f'<div class="stat-value">{org_stats.get("active_flags",0)}</div></div>'
        f'</div>'
    )

    # ── Renewal opportunities ────────────────────────────────────────────────
    if renewals:
        renewals_html = (
            '<div class="renewal-list">'
            + "".join(_renewal_row_html(f) for f in renewals[:8])
            + "</div>"
        )
    else:
        renewals_html = (
            '<p class="mi-empty">No contracts approaching renewal in the '
            f'next {config.RENEWAL_WINDOW_DAYS} days — awards data is building incrementally.</p>'
        )

    # ── Intelligence flags ───────────────────────────────────────────────────
    if other_flags:
        flags_html = (
            '<div class="flag-list">'
            + "".join(_flag_row_html(f) for f in other_flags[:10])
            + "</div>"
        )
    else:
        flags_html = (
            '<p class="mi-empty">No active intelligence flags — patterns will emerge '
            "as the awards database grows.</p>"
        )

    # ── Agency profiles ──────────────────────────────────────────────────────
    if agency_profiles:
        profiles_html = '<div class="profile-list">' + "".join(
            f'<div class="profile-item">'
            f'<div class="profile-name">{p.get("agency_name","Unknown")}</div>'
            f'<div class="profile-summary">{p.get("profile_summary","") or "<em>No narrative generated yet.</em>"}</div>'
            f'<div class="profile-meta">'
            f'<span>{p.get("total_notices",0)} notices</span>'
            f'<span>{p.get("total_awards",0)} awards</span>'
            f'<span>Renewal tendency: {p.get("renewal_tendency","?")}</span>'
            f'</div>'
            f'</div>'
            for p in agency_profiles[:6]
        ) + "</div>"
    else:
        profiles_html = (
            '<p class="mi-empty">Agency profiles are generated after '
            f'{config.AGENCY_PROFILE_MIN_NOTICES}+ notices are observed per agency.</p>'
        )

    return f"""
<div class="mi-section">
  {_MI_STYLE}
  <div class="mi-header">
    <div>
      <div class="mi-title">{config.LAYER2_SECTION_TITLE}</div>
      <div class="mi-subtitle">Knowledge graph · Contract awards · Pattern intelligence</div>
    </div>
    <div style="font-size:.75rem;color:var(--muted);">Updated {run_date.isoformat()}</div>
  </div>

  <!-- Knowledge graph stats -->
  <div class="mi-card" style="margin-bottom:1.25rem;">
    <div class="mi-card-header">
      <span class="mi-card-icon">&#9651;</span>
      <span class="mi-card-title">Knowledge Graph</span>
    </div>
    <div class="mi-card-body">{stats_html}</div>
  </div>

  <div class="mi-grid">

    <!-- Renewal opportunities -->
    <div class="mi-card">
      <div class="mi-card-header">
        <span class="mi-card-icon">&#8635;</span>
        <span class="mi-card-title">Contracts Approaching Renewal</span>
      </div>
      <div class="mi-card-body">{renewals_html}</div>
    </div>

    <!-- Intelligence flags -->
    <div class="mi-card">
      <div class="mi-card-header">
        <span class="mi-card-icon">&#9888;</span>
        <span class="mi-card-title">Intelligence Flags</span>
      </div>
      <div class="mi-card-body">{flags_html}</div>
    </div>

  </div>

  <!-- Agency profiles -->
  <div class="mi-card">
    <div class="mi-card-header">
      <span class="mi-card-icon">&#9632;</span>
      <span class="mi-card-title">Agency Intelligence Profiles</span>
    </div>
    <div class="mi-card-body">{profiles_html}</div>
  </div>

</div>
"""


def _inject_into_html(content: str, mi_html: str) -> str:
    """Inject the Market Intelligence section before </body>. Returns updated HTML."""
    if "</body>" not in content:
        return content + mi_html
    return content.replace("</body>", mi_html + "\n</body>", 1)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Procint Layer 2 pipeline")
    p.add_argument("--skip-awards",   action="store_true",
                   help="Skip GETS award notice scraping")
    p.add_argument("--skip-profiles", action="store_true",
                   help="Skip Claude agency profile generation")
    p.add_argument("--company",       type=str, default=None,
                   help="Firm name for competitor intelligence assessment")
    return p.parse_args()


def main(
    skip_awards: bool = False,
    skip_profiles: bool = False,
    company_name: str = None,
) -> None:
    from datetime import datetime
    start = datetime.now()

    logger.info("=" * 60)
    logger.info("Procint Layer 2 pipeline started at %s", start.isoformat())
    logger.info("=" * 60)

    # ── 1. Seed organisations from Layer 1 ──────────────────────────────────
    logger.info("--- Organisation seeding ---")
    seed_result = orgs_module.seed_from_layer1()
    logger.info("Seeding: %s", seed_result)

    # ── 2. Organisation discovery ────────────────────────────────────────────
    logger.info("--- Organisation discovery ---")
    new_orgs = discovery_module.run_discovery()
    logger.info("Discovery: %d new organisations", new_orgs)

    # ── 3. Awards ingestion ──────────────────────────────────────────────────
    if not skip_awards:
        logger.info("--- Awards ingestion ---")
        new_awards = awards_module.run_awards_ingestion()
        logger.info("Awards: %d new records", new_awards)
    else:
        logger.info("SKIPPED: Awards ingestion")

    # ── 4. Agency profiling ──────────────────────────────────────────────────
    if not skip_profiles:
        logger.info("--- Agency profiling ---")
        profiles_built = profiles_module.run_agency_profiling()
        logger.info("Profiles: %d built/refreshed", profiles_built)
    else:
        logger.info("SKIPPED: Agency profiling")

    # ── 5. Pattern detection ─────────────────────────────────────────────────
    logger.info("--- Pattern detection ---")
    flags = patterns_module.run_pattern_detection()
    logger.info("Patterns: %d flags generated", len(flags))

    # ── 6. HTML output ───────────────────────────────────────────────────────
    logger.info("--- Market Intelligence output ---")
    run_date = date.today()
    watchlist_filename = f"watchlist_{run_date.isoformat()}.html"

    # Fetch agency profiles for display
    agency_profile_rows = db.fetchall(
        """
        SELECT ap.*, o.name AS agency_name
          FROM agency_profiles ap
          JOIN organisations o ON o.org_id = ap.org_id
         ORDER BY ap.total_notices DESC
         LIMIT 6
        """
    )

    active_flags = patterns_module.get_active_flags(limit=30)
    discovery_stats = discovery_module.get_discovery_stats()

    mi_html = _build_market_intelligence_html(
        flags=active_flags,
        agency_profiles=agency_profile_rows,
        discovery_stats=discovery_stats,
        run_date=run_date,
    )

    row = db.load_output("watchlist_html", run_date, watchlist_filename)
    if row and row.get("content"):
        updated = _inject_into_html(row["content"], mi_html)
        db.save_output("watchlist_html", run_date, watchlist_filename, content=updated)
        logger.info("Market Intelligence section injected into DB: %s", watchlist_filename)
    else:
        # Write standalone MI file if Layer 1 HTML not present
        mi_filename = f"market_intelligence_{run_date.isoformat()}.html"
        mi_content = (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<style>:root{--bg:#0d1117;--surface:#161b22;--surf2:#1c2230;"
            "--border:#2a3344;--text:#e6edf3;--muted:#7d8fa8;}"
            "body{background:var(--bg);color:var(--text);"
            "font-family:system-ui;padding:2rem;}</style></head>"
            f"<body>{mi_html}</body></html>"
        )
        db.save_output(
            "market_intelligence_html", run_date, mi_filename, content=mi_content
        )
        logger.info("Standalone Market Intelligence written to DB: %s", mi_filename)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("=" * 60)
    logger.info("Layer 2 pipeline complete in %.1fs", elapsed)
    logger.info("=" * 60)


if __name__ == "__main__":
    args = parse_args()
    main(
        skip_awards=args.skip_awards,
        skip_profiles=args.skip_profiles,
        company_name=args.company,
    )
