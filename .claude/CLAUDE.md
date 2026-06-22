# Groundwork by BidEdge — Developer Context

## Product
Procurement intelligence SaaS for NZ government tenders.
Live at bidedge.co.nz. Pre-sales polish phase — not yet
at first paid client.

## Firm
BidEdge is the umbrella firm with three offerings:
- Groundwork: SaaS procurement intelligence
- Terrain: Fixed-price market opportunity scans
- Keystone: Executive decision support packs

## Stack
Python 3.12 (Railway) / 3.9 (local), Flask + Gunicorn,
PostgreSQL via Supabase (Singapore, transaction pooler,
port 6543), Claude API (claude-sonnet-4-6), APScheduler,
Railway hosting, Cloudflare DNS.

## Repo
github.com/robertandrewnz/procint
Local path: ~/Documents/GitHub/Procint
Deploy branch: main (Railway watches this)
CRITICAL: Always commit directly to main and push
immediately. Never create PRs or branches.
Never merge develop into main wholesale —
they have diverged 72+ commits. Cherry-pick only.

## Architecture

### Layer 1 — Daily intake
GETS scraper → parse → sector classify → composite
score → Claude enrichment → ACH bidder inference →
incumbent detection → watchlist output.
Runs 06:00 NZT daily via APScheduler.

### Layer 2 — Market intelligence
27,948 MBIE historical awards, supplier win profiles,
organisation profiling, agency profiling, pattern
detection, market signals generation.
Runs 07:00 NZT daily.

### Layer 3 — Artefacts on demand
Pursuit packages, competitor profiles, watch briefs,
demo artefacts, portal delivery.
Watch briefs run Monday 08:00 NZT.
Packages generated via portal or railway CLI.

## Key Files

### Application core
portal.py            — All Flask routes (public + portal +
                       admin). Single file, ~6,500 lines.
config.py            — All tunable parameters: model names,
                       scoring weights, thresholds, SMTP.
db.py                — DB connection, _SCHEMA_SQL bootstrap,
                       save_output / load_output helpers.
scheduler_railway.py — APScheduler job definitions for
                       Railway (5 jobs — see Scheduler section).

### Layer 1 pipeline
ingestion.py         — GETS RSS scraper and notice fetcher.
parsing.py           — Notice field extraction and normalisation.
scoring.py           — Composite score calculation.
enrichment.py        — Claude enrichment of notice text.
bidder_intelligence.py — ACH 5-stage bidder analysis pipeline
                         and incumbent detection (see below).
bidders.py           — Web search bidder identification;
                       government-entity filter.
canonical_suppliers.py — Supplier name normalisation and
                         deduplication.
output.py            — Watchlist HTML/MD/JSON report generation;
                       bidder card rendering with match_type badges.

### Layer 2 pipeline
layer2_pipeline.py   — Entry point: awards ingestion, org
                       seeding, agency profiling, pattern
                       detection, market intelligence.
awards.py            — MBIE award ingestion and querying.
organisations.py     — Canonical org names, aliases,
                       relationships, win history.
agency_profiles.py   — Claude-powered agency narrative profiles.
competitor_intel.py  — Market patterns, supplier win/loss streaks.
patterns.py          — Pattern flag detection across notices.
market_intelligence.py — Claude-powered user-specific market
                         signals (3 per user daily, via tool_use).
renewal_radar.py     — Contract Expiry Radar: upcoming renewals
                       predicted from MBIE award history.
preferences.py       — User preferences storage (sectors,
                       agencies, value thresholds).

### Layer 3 artefacts
pursuit_package.py   — Pursuit Intelligence Package generator.
                       Also owns _parse_incumbent_result(),
                       _store_incumbent_in_bidder_pool(),
                       _web_search_incumbent().
competitor_profile.py — Competitor Profile Report generator.
watch_brief.py       — Weekly Watch Brief HTML generator + mailer.
demo_package.py      — Demo Package generator (7 sectors).
win_position.py      — Multi-factor competitive position bands.

### Supporting
historical_data.py   — Supplier win history queries.
sector_classifier.py — Sector tag assignment.
mailer.py            — SMTP email delivery.
brand.py             — Brand assets and colour tokens.
storage.py           — Artefact file storage helpers.

## ACH Pipeline (bidder_intelligence.py)

5-stage pipeline run by generate_bidder_intelligence():

Stage 1 — Web search candidate identification
  - Sole firm identification source. Searches "[title] NZ
    suppliers" style queries. Returns up to 5 candidates.
  - Filters: government entities removed, garbage names
    removed (_is_garbage_name checks length, capitals,
    markdown, service-description phrases).
  - MBIE is NOT used for firm generation here.

Stage 2 — MBIE per-firm validation (metadata only)
  - Attaches total NZ govt wins, agency-specific wins,
    primary sector(s) per candidate.
  - Does NOT add/remove/rank firms.

Stage 3 — Requirements extraction
  - Claude (claude-haiku-4-5) extracts capabilities,
    statutory obligations, geographic scope, scale.

Stage 4 — ACH assessment
  - Claude (claude-haiku-4-5) ranks provided candidates
    using Analysis of Competing Hypotheses.
  - Returns ≤3 ranked bidders with probability band
    (High / Medium / Medium-Low / Low).
  - Strict: rejects any name not in the input candidate set.

Stage 5 — Post-hoc MBIE display badges
  - Applies category_match / unrelated_category / no_mbie
    badge to each result. Does NOT change Claude's ranking.

After ACH: _run_incumbent_detection_for_notice() runs a
separate web search to identify the current contract holder.

Batch runners:
  run_ach_for_enriched()      — re-run stale ACH rows
  run_ach_for_unprocessed()   — first-run only (no rows yet)
  run_incumbent_detection_all() — incumbent search for all
                                   watchlist notices

## Incumbent Detection

Confidence levels encoded via match_type in bidder_pool:

  high   → match_type = 'incumbent_identified'
           Badge: INCUMBENT (navy/teal)
           Condition: web search explicitly names the firm
           as current contract holder with a direct source.

  medium → match_type = 'incumbent_possible'
           Badge: "Possible incumbent — verify" (amber)
           Condition: strong sector association but no
           explicit contract naming (e.g. sector fallback
           hints like Babcock for defence).

  low    → not stored at all.

Staleness check: skips notice if incumbent_identified OR
incumbent_possible row already exists in bidder_pool.

Audit log: every detection attempt written to
incumbent_detection_log (notice_id, firm_found, stored bool,
error_message, run_at).

Manual tools:
  railway run python3 _store_incumbent.py <notice_id> <firm>
  railway run python3 _diag_incumbent_log.py [--notice <id>]
  railway run python3 _diag_incumbent_pool.py <notice_id>

## Bidder Pool — match_type Values

match_type            Display / meaning
──────────────────    ─────────────────────────────────────
ach_analysis          ACH pipeline output (Stages 1–5)
incumbent_identified  High-confidence current contract holder
incumbent_possible    Medium-confidence — needs verification
mbie_evidence         MBIE historical award match (same sector)
csv_inferred          CSV bidder list match (legacy Pipeline A)
web_inferred          Web search match (legacy Pipeline A)
exact / cross_sector  Legacy; may exist in old rows

ORDER BY in watchlist query:
  incumbent_identified → 0, incumbent_possible → 1,
  ach_analysis → 2, else → 3, then relevance_score DESC.

## Firm Name Validation (_is_garbage_name)

Rejects a candidate/stored firm name if it:
- Length > 60 characters
- Does not start with a capital letter
- Contains markdown characters: * # _ ` >
- Contains service-description phrases: "core service:",
  "service:", "services:", "operations and maintenance",
  "procurement", "contract"
- Contains error phrases: "unable to rank", "cannot identify",
  "no specific firm", "error:", "insufficient data for"

Applied at: Stage 1 candidate filtering, Stage 4 ACH
validation, and store_ach_results() before every INSERT.

## Market Signals (market_intelligence.py)

Generates 3 Claude-powered signals per user daily.
Uses tool_use with forced tool_choice (NOT json.loads) so
output is always structured. On failure: logs exception,
returns [], does NOT store an error fallback in the DB.

get_stored_signals() detects stale "pipeline error"
signals already in the DB and clears them before
regenerating so one bad run doesn't poison the full day.

## Database Rules

Always use transaction pooler URL, port 6543.
Never use session pooler or direct connection.
Always ADD COLUMN IF NOT EXISTS.
All artefact HTML must be stored in html_content
column in DB as well as written to disk.

Tables auto-created by db.ensure_tables() on startup:
  leads, pursuit_requests, competitor_requests,
  brief_sends, pipeline_runs, user_preferences,
  pipeline_outputs, package_documents,
  incumbent_detection_log, market_signals

Tables created by migrations (run once manually):
  organisations, name_aliases, mbie_award_notices,
  mbie_award_suppliers, mbie_award_categories,
  mbie_award_regions, agency_profiles, contract_awards,
  pattern_flags, relationships, sector_corrections

Tables assumed pre-existing in Supabase (not bootstrapped):
  raw_notices, parsed_notices, scored_notices,
  enriched_notices, bidder_pool, supplier_win_history,
  firm_sector_overrides

## Scheduler Jobs (scheduler_railway.py)

06:00 NZT (18:00 UTC) — Layer 1 full pipeline daily
07:00 NZT (19:00 UTC) — Layer 2 intelligence pipeline daily
Mon 08:00 NZT         — Weekly watch brief (generate + email)
First Sun 03:00 NZT   — Monthly procurement plan scraper
Hourly                — Stale-job watchdog (marks pipeline_runs
                        stuck >4 hours as failed)

## Admin Panel (/admin/pipeline)

All buttons POST then redirect (POST-Redirect-GET pattern).
pipeline_runs row inserted synchronously before thread starts
so auto-refresh triggers immediately on the GET.

Button                        Function called
────────────────────────────  ──────────────────────────────
Run Layer 1                   _run_layer1()
Run Layer 2                   layer2_pipeline.main()
ACH — Refresh stale           run_ach_for_enriched()
ACH — Catch up new            run_ach_for_unprocessed()
Incumbent Detection — All     run_incumbent_detection_all()
Generate Watch Briefs         _run_watch_brief()
Regenerate Demo Content       generate_demo_content.main(force=True)

## Railway / Filesystem

Railway filesystem is ephemeral — never rely on disk for
anything that must survive redeploy.
Railway Volume is not yet configured for /app/output.
To run scripts against production DB:
  railway run python3 script_name.py
(from ~/Documents/GitHub/Procint with railway linked to
comfortable-nurturing project)

## Diagnostic & One-Shot Scripts

_audit_bidder_quality.py   — Report/fix sector mismatches in
                             bidder_pool [--fix flag applies]
_audit_firm_sectors.py     — Audit supplier sector assignments
_backfill_overview_text.py — Re-fetch overview_text for nulls
_cleanup_bidder_canonical.py — Deduplicate canonical bidder names
_delete_bad_packages.py    — Remove malformed output packages
_diag_incumbent_log.py     — Query incumbent_detection_log
                             [--all | --notice <id>]
_diag_incumbent_pool.py    — Show bidder_pool rows for a notice
_diag_incumbent_34118228.py — One-off diagnostic (notice-specific)
_enrich_manual.py          — Manual enrichment for specific notices
_fix_34336969.py           — Delete bad ACH row; re-run ACH for
                             notice 34336969 (three waters O&M)
_fix_bidder_mismatches.py  — Purge and re-run bidder matching
                             for sector-exclusion mismatches
_migrate_bidder_pool.py    — One-time: clear legacy MBIE/CSV rows
_migrate_cyber_to_ict.py   — One-time: reclassify cybersecurity→ICT
_purge_heb_nzdf.py         — One-off: purge test data
_store_incumbent.py        — Manual incumbent store:
                             python3 _store_incumbent.py <id> <firm>
_test_incumbent.py         — Incumbent detection diagnostic

## Branding

Firm: BidEdge
Product: Groundwork by BidEdge
Colours: Navy #1E2D40, Teal #2A9D8F
Logo: inline SVG in nav (bidedge-nav.svg)
Taglines:
- BidEdge: "Most organisations act on incomplete
  intelligence. Know before you bid. Know before
  you enter. Know before you decide."
- Groundwork: "Know before you bid. Win when you do."
- Terrain: "Know the ground before you move."
- Keystone: "Every signal. One decision agenda."

## Pricing

Groundwork: Watch $4,900/yr, Pursue $9,900/yr, Edge custom
Terrain: $6,500 + GST fixed price, 10 business days
Keystone: From $8,500 + GST, retainer options available

## Demo Artefacts

7 sectors with fictional firms:
FM           → Cityworks NZ
Cybersecurity → Sentinel Digital (competitor: Datacom)
Construction → Meridian Civil (competitor: Fletcher)
Defence      → Apex Engineering (competitor: Nova Systems)
ICT          → Korepath Systems
Infrastructure → Southern Civil Group (competitor: Downer)
Health       → MedTech Solutions NZ (competitor: F&P Healthcare)

Demo rules:
- Each sector must use sector-matched notices only
- Win position must be Competitive or Conditional Go
- Never generate demos via HTTP admin routes
- Call generation functions directly or via railway run

## Win Position Bands

Strong / Competitive / Conditional Go / Challenging /
Not Recommended
Never show Challenging or Not Recommended in a demo.

## Critical Principles

- Confident wrong results are worse than no result
- Demo artefacts must show correct-sector content —
  wrong sector content kills a sales conversation
- Never create PRs or branches — commit to main
- Always run pre-work review before any changes
- Always verify after changes before committing
- Never run generation scripts via HTTP requests
- Firm names must pass _is_garbage_name validation
  before being stored in bidder_pool
- Market signals use tool_use, never json.loads
- Incumbent confidence must be high/medium/low —
  never store low-confidence results
