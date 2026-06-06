-- Migration 004 — User preferences + market signals
-- Run once against the Supabase/PostgreSQL database.

-- ── User preferences ──────────────────────────────────────────────────────────
-- Keyed by username string (matches portal_config.json client keys).
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id         TEXT        PRIMARY KEY,
    sectors         TEXT[]      NOT NULL DEFAULT '{}',
    agency_focus    TEXT[]      NOT NULL DEFAULT '{}',
    min_value_nzd   INTEGER     NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- ── Market signals ────────────────────────────────────────────────────────────
-- Claude-generated, per-user, regenerated daily.
CREATE TABLE IF NOT EXISTS market_signals (
    id              SERIAL      PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    signal          TEXT        NOT NULL,
    priority        TEXT        NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    action          TEXT        NOT NULL,
    generated_at    TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_signals_user_generated
    ON market_signals (user_id, generated_at DESC);
