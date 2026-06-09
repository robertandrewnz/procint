-- Migration 010: Enable Row Level Security on all public tables
-- Adds a service-role bypass policy to every table so the Flask app
-- (which connects via DATABASE_URL with the service role) retains full
-- access, while direct anon/authenticated Supabase clients are blocked
-- by default until explicit policies are added.
--
-- Run:  psql $DATABASE_URL -f migrations/010_enable_rls.sql
--
-- Safe to re-run: ALTER TABLE ... ENABLE ROW LEVEL SECURITY is idempotent;
-- DROP POLICY IF EXISTS prevents duplicate-policy errors.
--
-- Tables covered (SELECT tablename FROM pg_tables WHERE schemaname = 'public'):
--   agency_profiles, bidder_pool, brief_sends, competitor_requests,
--   contract_awards, leads, market_signals, mbie_award_categories,
--   mbie_award_notices, mbie_award_regions, mbie_award_suppliers,
--   name_aliases, organisations, parsed_notices, pattern_flags,
--   pipeline_outputs, pipeline_runs, pursuit_requests, raw_notices,
--   relationships, scored_notices, sector_corrections, user_preferences
-- Note: supplier_win_history is a materialised view — RLS not applicable.


-- ── agency_profiles ───────────────────────────────────────────────────────────
ALTER TABLE agency_profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON agency_profiles;
CREATE POLICY "Service role full access" ON agency_profiles
    USING (auth.role() = 'service_role');

-- ── bidder_pool ───────────────────────────────────────────────────────────────
ALTER TABLE bidder_pool ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON bidder_pool;
CREATE POLICY "Service role full access" ON bidder_pool
    USING (auth.role() = 'service_role');

-- ── brief_sends ───────────────────────────────────────────────────────────────
ALTER TABLE brief_sends ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON brief_sends;
CREATE POLICY "Service role full access" ON brief_sends
    USING (auth.role() = 'service_role');

-- ── competitor_requests ───────────────────────────────────────────────────────
ALTER TABLE competitor_requests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON competitor_requests;
CREATE POLICY "Service role full access" ON competitor_requests
    USING (auth.role() = 'service_role');

-- ── contract_awards ───────────────────────────────────────────────────────────
ALTER TABLE contract_awards ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON contract_awards;
CREATE POLICY "Service role full access" ON contract_awards
    USING (auth.role() = 'service_role');

-- ── leads ─────────────────────────────────────────────────────────────────────
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON leads;
CREATE POLICY "Service role full access" ON leads
    USING (auth.role() = 'service_role');

-- ── market_signals ────────────────────────────────────────────────────────────
ALTER TABLE market_signals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON market_signals;
CREATE POLICY "Service role full access" ON market_signals
    USING (auth.role() = 'service_role');

-- ── mbie_award_categories ────────────────────────────────────────────────────
ALTER TABLE mbie_award_categories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON mbie_award_categories;
CREATE POLICY "Service role full access" ON mbie_award_categories
    USING (auth.role() = 'service_role');

-- ── mbie_award_notices ────────────────────────────────────────────────────────
ALTER TABLE mbie_award_notices ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON mbie_award_notices;
CREATE POLICY "Service role full access" ON mbie_award_notices
    USING (auth.role() = 'service_role');

-- ── mbie_award_regions ────────────────────────────────────────────────────────
ALTER TABLE mbie_award_regions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON mbie_award_regions;
CREATE POLICY "Service role full access" ON mbie_award_regions
    USING (auth.role() = 'service_role');

-- ── mbie_award_suppliers ──────────────────────────────────────────────────────
ALTER TABLE mbie_award_suppliers ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON mbie_award_suppliers;
CREATE POLICY "Service role full access" ON mbie_award_suppliers
    USING (auth.role() = 'service_role');

-- ── name_aliases ──────────────────────────────────────────────────────────────
ALTER TABLE name_aliases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON name_aliases;
CREATE POLICY "Service role full access" ON name_aliases
    USING (auth.role() = 'service_role');

-- ── organisations ─────────────────────────────────────────────────────────────
ALTER TABLE organisations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON organisations;
CREATE POLICY "Service role full access" ON organisations
    USING (auth.role() = 'service_role');

-- ── parsed_notices ────────────────────────────────────────────────────────────
ALTER TABLE parsed_notices ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON parsed_notices;
CREATE POLICY "Service role full access" ON parsed_notices
    USING (auth.role() = 'service_role');

-- ── pattern_flags ─────────────────────────────────────────────────────────────
ALTER TABLE pattern_flags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON pattern_flags;
CREATE POLICY "Service role full access" ON pattern_flags
    USING (auth.role() = 'service_role');

-- ── pipeline_outputs ──────────────────────────────────────────────────────────
ALTER TABLE pipeline_outputs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON pipeline_outputs;
CREATE POLICY "Service role full access" ON pipeline_outputs
    USING (auth.role() = 'service_role');

-- ── pipeline_runs ─────────────────────────────────────────────────────────────
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON pipeline_runs;
CREATE POLICY "Service role full access" ON pipeline_runs
    USING (auth.role() = 'service_role');

-- ── pursuit_requests ──────────────────────────────────────────────────────────
ALTER TABLE pursuit_requests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON pursuit_requests;
CREATE POLICY "Service role full access" ON pursuit_requests
    USING (auth.role() = 'service_role');

-- ── raw_notices ───────────────────────────────────────────────────────────────
ALTER TABLE raw_notices ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON raw_notices;
CREATE POLICY "Service role full access" ON raw_notices
    USING (auth.role() = 'service_role');

-- ── relationships ─────────────────────────────────────────────────────────────
ALTER TABLE relationships ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON relationships;
CREATE POLICY "Service role full access" ON relationships
    USING (auth.role() = 'service_role');

-- ── scored_notices ────────────────────────────────────────────────────────────
ALTER TABLE scored_notices ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON scored_notices;
CREATE POLICY "Service role full access" ON scored_notices
    USING (auth.role() = 'service_role');

-- ── sector_corrections ────────────────────────────────────────────────────────
ALTER TABLE sector_corrections ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON sector_corrections;
CREATE POLICY "Service role full access" ON sector_corrections
    USING (auth.role() = 'service_role');

-- ── user_preferences ──────────────────────────────────────────────────────────
ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON user_preferences;
CREATE POLICY "Service role full access" ON user_preferences
    USING (auth.role() = 'service_role');


-- ── Verification query ────────────────────────────────────────────────────────
-- Run after applying to confirm all tables have RLS enabled and policies set:
--
-- SELECT t.tablename,
--        t.rowsecurity AS rls_enabled,
--        p.policyname
--   FROM pg_tables t
--   LEFT JOIN pg_policies p
--     ON p.tablename = t.tablename
--    AND p.schemaname = 'public'
--    AND p.policyname = 'Service role full access'
--  WHERE t.schemaname = 'public'
--  ORDER BY t.tablename;
