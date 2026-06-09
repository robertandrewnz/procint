-- Migration 004: pipeline_outputs — DB-backed file storage for Railway deployments
-- Replaces all output/ filesystem writes so generated HTML/JSON/MD/PDF persists
-- across ephemeral Railway runs.
--
-- output_type values used by the pipeline:
--   watchlist_html       — Layer 1 daily HTML watchlist (may be updated by Layer 2 MI injection)
--   watchlist_json       — Layer 1 daily JSON watchlist
--   watchlist_md         — Layer 1 daily Markdown watchlist
--   market_intelligence_html — Layer 2 standalone MI page (when no Layer 1 HTML exists)
--   pursuit_package      — Layer 3 pursuit intelligence package (per client/notice)
--   watch_brief          — Layer 3 weekly executive briefing
--   competitor_profile   — Layer 3 competitor intelligence report
--   demo_html            — Layer 3 branded demo HTML
--   demo_pdf             — Layer 3 branded demo PDF bytes
--   mbie_metadata        — refresh_mbie.py header cache (run_date = 1970-01-01 sentinel)
--
-- NOTE: PDF files (demo_pdf) can reach 1–3 MB. If storage costs become a concern,
--       move demo_pdf rows to Supabase Storage and store only the Storage path here.
--       MBIE CSV source files (5–50 MB each) are NOT stored here; they are downloaded
--       to /tmp during a run, ingested, then discarded. They are Supabase Storage
--       candidates if offline re-ingestion is required.

CREATE TABLE IF NOT EXISTS pipeline_outputs (
    id            SERIAL PRIMARY KEY,
    output_type   TEXT    NOT NULL,
    run_date      DATE    NOT NULL DEFAULT CURRENT_DATE,
    filename      TEXT    NOT NULL,
    content       TEXT,           -- HTML / JSON / Markdown (NULL for binary)
    content_bytes BYTEA,          -- PDF or other binary (NULL for text)
    client_slug   TEXT,           -- set for artefacts scoped to a client
    notice_id     TEXT,           -- set for pursuit packages and demo packages
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (output_type, run_date, filename)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_outputs_type_date
    ON pipeline_outputs (output_type, run_date DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_outputs_client
    ON pipeline_outputs (client_slug, run_date DESC)
    WHERE client_slug IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pipeline_outputs_notice
    ON pipeline_outputs (notice_id)
    WHERE notice_id IS NOT NULL;
