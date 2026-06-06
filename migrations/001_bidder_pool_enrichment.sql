-- Migration 001: Add relevance scoring, reasoning, and Claude context to bidder_pool
-- Run once against an existing database that already has the Layer 1 schema.
-- Safe to re-run (uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS patterns).

ALTER TABLE bidder_pool
    ADD COLUMN IF NOT EXISTS relevance_score  NUMERIC(5,4),  -- 0.0000–1.0000
    ADD COLUMN IF NOT EXISTS match_type       TEXT,          -- exact / cross_sector
    ADD COLUMN IF NOT EXISTS reasoning        TEXT,          -- pipe-separated bullet points
    ADD COLUMN IF NOT EXISTS company_context  TEXT,          -- Claude 2-sentence profile
    ADD COLUMN IF NOT EXISTS context_confidence TEXT;        -- high / medium / low / unknown

COMMENT ON COLUMN bidder_pool.relevance_score IS
    'Keyword cosine-similarity score between notice text and bidder notes+sector-keywords. 0=no match, 1=full overlap.';

COMMENT ON COLUMN bidder_pool.match_type IS
    'exact: bidder sector tag matches notice sector tag directly. cross_sector: matched via keyword relevance only.';

COMMENT ON COLUMN bidder_pool.reasoning IS
    'Pipe-separated short reasoning bullets explaining why this bidder was included. Displayed on watchlist cards.';

COMMENT ON COLUMN bidder_pool.company_context IS
    'Claude-generated 2-sentence company profile explaining what the firm does and why credible for this notice.';

COMMENT ON COLUMN bidder_pool.context_confidence IS
    'Claude self-reported confidence in the company_context. low = firm may be misidentified or profile may be stale.';
