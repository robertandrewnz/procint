"""
intel_library — Strategic intelligence document library for Groundwork by BidEdge.

Monitors NZ government strategic documents, extracts procurement signals,
and feeds context into Layer 1 scoring and Layer 3 artefact generation.

Modules:
  schema.sql              — Database tables and views (migration 005)
  seed_sources.py         — Populate intel_sources and intel_categories
  extract_signals.py      — Fetch documents, extract signals via Claude
  scoring_integration.py  — Layer 1: get_strategic_score_boost()
  layer3_integration.py   — Layer 3: build_strategic_context()
  scheduler_jobs.py       — Scheduled refresh jobs
"""
