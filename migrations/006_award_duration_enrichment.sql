-- Migration 006: Add duration enrichment columns to mbie_award_notices
-- and sector_tag to contract_awards (which already has the column but
-- no index on the enriched path).
--
-- Run: psql $DATABASE_URL -f migrations/006_award_duration_enrichment.sql

-- ── mbie_award_notices enrichment columns ────────────────────────────────────
ALTER TABLE mbie_award_notices
    ADD COLUMN IF NOT EXISTS contract_duration_months INTEGER,
    ADD COLUMN IF NOT EXISTS contract_expiry          DATE,
    ADD COLUMN IF NOT EXISTS sector_tag               TEXT;

CREATE INDEX IF NOT EXISTS idx_mbie_notices_expiry
    ON mbie_award_notices(contract_expiry)
    WHERE contract_expiry IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mbie_notices_sector_expiry
    ON mbie_award_notices(sector_tag, contract_expiry)
    WHERE contract_expiry IS NOT NULL AND sector_tag IS NOT NULL;

-- ── contract_awards: ensure sector_tag index exists (column already present) ─
CREATE INDEX IF NOT EXISTS idx_contract_awards_expiry
    ON contract_awards(end_date)
    WHERE end_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contract_awards_sector_expiry
    ON contract_awards(sector_tag, end_date)
    WHERE end_date IS NOT NULL AND sector_tag IS NOT NULL;
