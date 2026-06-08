-- Migration 005 — Intel Library
-- Strategic intelligence document library for Groundwork by BidEdge.
-- Run once against the Supabase/PostgreSQL database.
-- psql $DATABASE_URL -f intel_library/schema.sql

-- ── Categories ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS intel_categories (
    id          SERIAL      PRIMARY KEY,
    name        TEXT        NOT NULL,
    icon        TEXT,
    description TEXT
);

-- ── Sources ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS intel_sources (
    id                      SERIAL      PRIMARY KEY,
    category_id             INTEGER     REFERENCES intel_categories(id),
    title                   TEXT        NOT NULL,
    short_name              TEXT,
    publisher               TEXT,
    url                     TEXT,
    pdf_url                 TEXT,
    document_type           TEXT        NOT NULL
        CHECK (document_type IN ('policy','forecast','strategy','report','guidance','news','speech')),
    update_frequency        TEXT,
    nz_relevance_score      INTEGER     CHECK (nz_relevance_score BETWEEN 1 AND 10),
    procurement_relevance   TEXT[],
    notes                   TEXT,
    is_active               BOOLEAN     NOT NULL DEFAULT TRUE,
    last_checked            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_intel_sources_category
    ON intel_sources (category_id);
CREATE INDEX IF NOT EXISTS idx_intel_sources_active
    ON intel_sources (is_active);

-- ── Snapshots ─────────────────────────────────────────────────────────────────
-- One row per fetch. version_hash (SHA-256 of raw_text) detects content changes.

CREATE TABLE IF NOT EXISTS intel_snapshots (
    id              SERIAL      PRIMARY KEY,
    source_id       INTEGER     NOT NULL REFERENCES intel_sources(id),
    snapshot_date   DATE        NOT NULL DEFAULT CURRENT_DATE,
    raw_text        TEXT,
    summary         TEXT,
    key_signals     JSONB,
    version_hash    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intel_snapshots_source
    ON intel_snapshots (source_id, snapshot_date DESC);

-- ── Signals ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS intel_signals (
    id                  SERIAL      PRIMARY KEY,
    snapshot_id         INTEGER     REFERENCES intel_snapshots(id),
    source_id           INTEGER     NOT NULL REFERENCES intel_sources(id),
    signal_type         TEXT        NOT NULL
        CHECK (signal_type IN ('budget_increase','policy_change','new_initiative','risk','opportunity')),
    signal_title        TEXT        NOT NULL,
    signal_body         TEXT,
    affected_sectors    TEXT[],
    affected_agencies   TEXT[],
    dollar_value        BIGINT,
    timeframe           TEXT,
    confidence          TEXT        NOT NULL CHECK (confidence IN ('high','medium','low')),
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intel_signals_source
    ON intel_signals (source_id, extracted_at DESC);
CREATE INDEX IF NOT EXISTS idx_intel_signals_sectors
    ON intel_signals USING GIN (affected_sectors);
CREATE INDEX IF NOT EXISTS idx_intel_signals_agencies
    ON intel_signals USING GIN (affected_agencies);

-- ── Sector profiles ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS intel_sector_profiles (
    id                      SERIAL      PRIMARY KEY,
    sector                  TEXT        NOT NULL UNIQUE,
    government_spend_annual BIGINT,
    pipeline_value          BIGINT,
    top_agencies            TEXT[],
    dominant_suppliers      TEXT[],
    policy_drivers          TEXT[],
    risk_factors            TEXT[],
    opportunity_factors     TEXT[],
    last_updated            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Agency profiles ───────────────────────────────────────────────────────────
-- Intel library layer — supplementary to Layer 2 agency_profiles table.

CREATE TABLE IF NOT EXISTS intel_agency_profiles (
    id                          SERIAL      PRIMARY KEY,
    agency_name                 TEXT        NOT NULL UNIQUE,
    short_name                  TEXT,
    category                    TEXT,
    annual_procurement_value    BIGINT,
    primary_sectors             TEXT[],
    procurement_style           TEXT,
    strategic_priorities        TEXT[],
    last_updated                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Usage tracking ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS intel_source_usage (
    id                  SERIAL      PRIMARY KEY,
    source_id           INTEGER     NOT NULL REFERENCES intel_sources(id),
    used_in             TEXT,
    usage_type          TEXT        NOT NULL
        CHECK (usage_type IN (
            'scoring_boost','pursuit_package','competitor_profile',
            'watch_brief','sector_profile','signal_extracted'
        )),
    significance_score  INTEGER     CHECK (significance_score BETWEEN 1 AND 10),
    signal_ids          INTEGER[],
    used_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intel_source_usage_source
    ON intel_source_usage (source_id, used_at DESC);
CREATE INDEX IF NOT EXISTS idx_intel_source_usage_type
    ON intel_source_usage (usage_type);

-- ── Views ─────────────────────────────────────────────────────────────────────

-- Active signals joined to their source and category.
CREATE OR REPLACE VIEW v_active_signals AS
SELECT
    sig.id,
    sig.snapshot_id,
    sig.signal_type,
    sig.signal_title,
    sig.signal_body,
    sig.affected_sectors,
    sig.affected_agencies,
    sig.dollar_value,
    sig.timeframe,
    sig.confidence,
    sig.extracted_at,
    src.id          AS source_id,
    src.title       AS source_title,
    src.short_name  AS source_short_name,
    src.publisher,
    src.nz_relevance_score,
    cat.name        AS category_name
FROM intel_signals sig
JOIN intel_sources src ON src.id = sig.source_id
LEFT JOIN intel_categories cat ON cat.id = src.category_id
WHERE src.is_active = TRUE
ORDER BY sig.extracted_at DESC;

-- Sector context: sector profile + recent signal summary (last 90 days).
CREATE OR REPLACE VIEW v_sector_context AS
SELECT
    sp.sector,
    sp.government_spend_annual,
    sp.pipeline_value,
    sp.top_agencies,
    sp.dominant_suppliers,
    sp.policy_drivers,
    sp.risk_factors,
    sp.opportunity_factors,
    sp.last_updated,
    COALESCE(sig_agg.recent_signal_titles, ARRAY[]::TEXT[]) AS recent_signal_titles,
    COALESCE(sig_agg.recent_signal_count, 0) AS recent_signal_count
FROM intel_sector_profiles sp
LEFT JOIN LATERAL (
    SELECT
        ARRAY_AGG(DISTINCT sig.signal_title ORDER BY sig.signal_title) AS recent_signal_titles,
        COUNT(sig.id) AS recent_signal_count
    FROM intel_signals sig
    JOIN intel_sources src ON src.id = sig.source_id
    WHERE src.is_active = TRUE
      AND sig.affected_sectors @> ARRAY[sp.sector]
      AND sig.extracted_at >= NOW() - INTERVAL '90 days'
) sig_agg ON TRUE;

-- Usage summary per source.
CREATE OR REPLACE VIEW v_source_usage_summary AS
WITH type_counts AS (
    SELECT source_id, usage_type, COUNT(*) AS cnt
    FROM intel_source_usage
    GROUP BY source_id, usage_type
),
breakdown AS (
    SELECT
        source_id,
        JSONB_OBJECT_AGG(usage_type, cnt) AS usage_breakdown
    FROM type_counts
    GROUP BY source_id
),
totals AS (
    SELECT
        source_id,
        COUNT(*)                                        AS total_references,
        ROUND(AVG(significance_score)::NUMERIC, 1)     AS avg_significance,
        MAX(used_at)                                    AS last_used
    FROM intel_source_usage
    GROUP BY source_id
)
SELECT
    src.id                                          AS source_id,
    src.title                                       AS source_title,
    src.short_name,
    src.publisher,
    COALESCE(t.total_references, 0)                 AS total_references,
    t.avg_significance,
    t.last_used,
    COALESCE(b.usage_breakdown, '{}'::JSONB)        AS usage_breakdown
FROM intel_sources src
LEFT JOIN totals   t ON t.source_id = src.id
LEFT JOIN breakdown b ON b.source_id = src.id;
