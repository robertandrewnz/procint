-- Migration 011: overview_text and structured key dates from notice pages
-- Safe to re-run (ADD COLUMN IF NOT EXISTS throughout).

-- Full narrative body from the GETS notice detail page.
-- Stored separately from the legacy `description` field which used
-- CSS selectors that often failed to match the GETS HTML.
ALTER TABLE raw_notices
    ADD COLUMN IF NOT EXISTS overview_text TEXT;

-- Structured key dates extracted from overview_text via regex in parsing.py.
-- All nullable — NULL means not found, not absent from the notice.
ALTER TABLE parsed_notices
    ADD COLUMN IF NOT EXISTS briefing_date         DATE,
    ADD COLUMN IF NOT EXISTS questions_deadline    DATE,
    ADD COLUMN IF NOT EXISTS registration_deadline DATE,
    ADD COLUMN IF NOT EXISTS procurement_stage     TEXT;
