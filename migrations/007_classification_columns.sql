-- Migration 007 — Hybrid sector classification tracking
-- Adds per-notice columns to record how each notice was classified and whether
-- it needs human review.  Also creates the sector_corrections table for audit
-- trail and keyword-improvement feedback.

-- ── parsed_notices additions ─────────────────────────────────────────────────

ALTER TABLE parsed_notices
    ADD COLUMN IF NOT EXISTS classification_method     TEXT,        -- 'keyword' | 'claude' | 'human'
    ADD COLUMN IF NOT EXISTS classification_confidence TEXT,        -- 'high' | 'medium' | 'low'
    ADD COLUMN IF NOT EXISTS classification_reasoning  TEXT,        -- one-sentence rationale (claude/human only)
    ADD COLUMN IF NOT EXISTS needs_sector_review       BOOLEAN NOT NULL DEFAULT FALSE;

-- Index: quick lookup of notices awaiting review
CREATE INDEX IF NOT EXISTS idx_parsed_needs_review
    ON parsed_notices (needs_sector_review)
    WHERE needs_sector_review = TRUE;

-- ── Sector corrections (human feedback loop) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS sector_corrections (
    id               SERIAL PRIMARY KEY,
    notice_id        TEXT        NOT NULL REFERENCES parsed_notices (notice_id) ON DELETE CASCADE,
    original_sector  TEXT        NOT NULL,
    corrected_sector TEXT        NOT NULL,
    corrected_by     TEXT        NOT NULL,   -- username of admin who made the correction
    corrected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note             TEXT                    -- optional admin note / reason
);

CREATE INDEX IF NOT EXISTS idx_sector_corrections_notice
    ON sector_corrections (notice_id);
